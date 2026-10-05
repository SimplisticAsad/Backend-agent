# ac.employee.update  (requirements: requirement.employee_directory; operations: operation.employee.update)
Feature: Update Employee succeeds

  Scenario: Update Employee succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Change the Employee details
    And Update Employee
    Then The changes to the Employee are saved
